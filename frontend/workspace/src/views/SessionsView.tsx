import { useEffect, useState } from "react";
import { Icon, Empty } from "../ui";
import { api, usd, clock } from "../data";
import { SegmentedControl } from "../components/SegmentedControl";
import { StatusBadge, sessionStatus } from "../components/StatusBadge";
import { StatsGrid, Skeleton, InlineAlert } from "../components/feedback";
import { RichPopover, ViewSwitch } from "../components/fields";
import { DataTable } from "../components/DataTable";
import { SessionProgress } from "../components/SessionProgress";
import type { Session } from "../types";
import type { SessionsProps } from "../viewTypes";
export function SessionsView({ owner, metrics, stale, filtered, filter, setFilter, now, w, navigate, setStop, setSelected, setWalletModal }: SessionsProps) {
  const [preference, setPreference] = useState<{ owner?: string; compact: boolean }>({ compact: false });
  const [checking, setChecking] = useState<string | null>(null);
  const [error, setError] = useState<{ id: string; message: string } | null>(null);
  useEffect(() => {
    let compact = false;
    try { compact = !!owner && localStorage.getItem("zeus-ui-compact:" + owner) === "true"; } catch {}
    setPreference({ owner, compact }); setError(null); setChecking(null);
  }, [owner]);
  const compact = preference.owner === owner && preference.compact;
  const toggleCompact = (value: boolean) => {
    setPreference({ owner, compact: value });
    if (owner) try { localStorage.setItem("zeus-ui-compact:" + owner, String(value)); } catch {}
  };
  const actions = (s: Session) => <div className="row-actions">
    <button className="button subtle" onClick={() => setSelected(s)}>Détails<Icon name="arrow" size={14} /></button>
    {s.running ? <button className="button danger small" onClick={() => setStop(s)}><Icon name="stop" size={13} />Arrêter</button> : !s.cleanup_confirmed ?
      <button className="button secondary small" disabled={!!checking} aria-busy={checking === s.id || undefined} onClick={async () => {
        setChecking(s.id); setError(null);
        try { await api("/reconcile", {}); w.refresh(); } catch (e: any) { setError({ id: s.id, message: e.message }); } finally { setChecking(null); }
      }}>{checking === s.id ? "Vérification…" : "Vérifier l’arrêt"}</button> : null}
  </div>;
  return <>
    <div className="page-heading"><div><span className="eyebrow">ACTIVITÉ DU COMPTE</span><h1>Vos sessions.</h1></div>
      <button className="button primary compact" onClick={() => navigate("run")}><Icon name="run" size={16} />Nouvelle session</button></div>
    {owner ? <>
      {!w.snapshot ? <Skeleton /> : <StatsGrid items={[
        { label: "Volume exécuté", value: stale ? "—" : usd(metrics?.volume_usd), icon: "chart" },
        { label: "PnL réalisé net", value: stale ? "—" : usd(metrics?.net_pnl), icon: "target", negative: !stale && (metrics?.net_pnl ?? 0) < 0 },
        { label: "Fills", value: stale ? "—" : metrics?.fills ?? "—", icon: "auto" },
        { label: "Coût / 1M", value: stale ? "—" : usd(metrics?.cost_per_1m), icon: "info", help: <RichPopover label="Coût par million">PnL réalisé net négatif rapporté au volume exécuté, multiplié par un million. Les positions ouvertes ne sont pas incluses.</RichPopover> },
      ]} />}
      <div className="list-toolbar"><SegmentedControl label="Afficher les sessions" value={filter}
        options={[{ value: "active", label: "En cours" }, { value: "closed", label: "Terminées" }, { value: "all", label: "Toutes" }]} onValueChange={setFilter} />
        <ViewSwitch checked={compact} onChange={toggleCompact} /><span>{filtered.length} session{filtered.length > 1 ? "s" : ""}</span></div>
      {filtered.length ? compact ? <div className="panel compact-sessions"><DataTable<Session> rows={filtered} rowKey={(s) => s.id} caption="Sessions du compte connecté" columns={[
        { id: "name", label: "Marché", render: (s) => <strong>{s.name}</strong>, sort: (s) => s.name },
        { id: "state", label: "État", render: (s) => <StatusBadge tone={sessionStatus(s, stale).tone}>{sessionStatus(s, stale).label}</StatusBadge> },
        { id: "volume", label: "Volume", render: (s) => usd(s.volume_usd), sort: (s) => s.volume_usd },
        { id: "fills", label: "Fills", render: (s) => s.fills ?? "—", sort: (s) => s.fills },
        { id: "cost", label: "Coût / 1M", render: (s) => usd(s.cost_per_1m), sort: (s) => s.cost_per_1m },
        { id: "actions", label: "Actions", render: actions },
      ]} />{error && <InlineAlert>{error.message}</InlineAlert>}</div> : <div className="session-list">
        {filtered.map((s) => { const status = sessionStatus(s, stale); return <article className={"panel session-card " + (status.tone === "online" ? "is-running" : "")} key={s.id}>
          <div className="session-card-head"><div className="session-title"><span className="session-symbol"><Icon name={s.strategy === "outcome" ? "auto" : s.strategy === "mid" ? "target" : "book"} size={22} /></span>
            <div><div className="session-tags"><span>{s.strategy === "outcome" ? "AUTO OUTCOME" : s.strategy.toUpperCase()}</span><span>Maker</span></div>
              <h2>{s.name}{s.config.outcome_side && <small> · {s.config.outcome_side === "yes" ? "Yes" : "No"}</small>}</h2></div></div>
            <StatusBadge tone={status.tone}>{status.label}</StatusBadge></div>
          <div className="session-metrics"><div><span>Volume</span><strong>{usd(s.volume_usd)}</strong></div>
            <div><span>Fills</span><strong>{s.fills ?? "—"}</strong></div><div><span>Net réalisé</span><strong className={s.net_pnl != null && s.net_pnl < 0 ? "negative" : s.net_pnl != null && s.net_pnl > 0 ? "positive" : ""}>{usd(s.net_pnl)}</strong></div>
            <div><span>Durée</span><strong>{clock(s.duration_sec + (s.running && !stale && !s.stale ? Math.max(0, now - w.received) / 1000 : 0))}</strong></div></div>
          <SessionProgress session={s} stale={stale} extraSeconds={Math.max(0, now - w.received) / 1000} />
          <div className="session-footer"><span>{usd(s.config.order_size_usd ?? s.config.order_usd)} / ordre{s.rotations > 0 && " · " + s.rotations + " rotations"}</span>{actions(s)}</div>
          {s.reason && <p className="session-reason"><Icon name="info" size={14} />{s.reason}</p>}
          {error?.id === s.id && <InlineAlert>{error.message}</InlineAlert>}
        </article>; })}
      </div> : !w.snapshot ? <p className="hint">Vérification des sessions…</p> : <Empty title={filter === "active" ? "Tout est prêt pour commencer." : "Aucune session ici."}
        text={filter === "active" ? "Vos sessions apparaîtront ici dès leur lancement." : "Les sessions vérifiées restent accessibles dans cet espace."}
        action={<button className="button secondary" onClick={() => navigate("run")}>Préparer une session<Icon name="arrow" size={16} /></button>} />}
    </> : <Empty icon="wallet" title="Votre activité, au même endroit." text="Connectez votre wallet pour retrouver vos sessions et leurs résultats."
      action={<button className="button primary" onClick={() => setWalletModal(true)}>Connecter mon wallet</button>} />}
  </>;
}
