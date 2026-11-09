import { useState } from "react";
import { Menu } from "@base-ui/react/menu";
import { Popover } from "@base-ui/react/popover";
import { Icon } from "../../icons";
import { short, usd } from "../../data";
import { StatusBadge } from "../StatusBadge";
import { Skeleton } from "../feedback";

// Layro: educlopez/user-account-avatar — address identity with an optional account popover.
export function AccountAvatar({ address, interactive = false }: { address: string; interactive?: boolean }) {
  const glyph = <span className="account-avatar" aria-hidden="true">{address.slice(2, 4).toUpperCase()}</span>;
  if (!interactive) return glyph;
  return <Popover.Root><Popover.Trigger className="avatar-trigger" aria-label="Identité du compte" data-layro="educlopez/user-account-avatar">{glyph}</Popover.Trigger>
    <Popover.Portal><Popover.Positioner sideOffset={8} className="popover-positioner"><Popover.Popup className="rich-popover">
      <Popover.Title>Compte connecté</Popover.Title><Popover.Description className="address">{address}</Popover.Description>
    </Popover.Popup></Popover.Positioner></Popover.Portal>
  </Popover.Root>;
}
// Layro: kokonut-labs/profile-dropdown — one connected account, action slots, no demo accounts.
export function ProfileDropdown({ address, onAccount, onLogout, notify }: { address: string; onAccount: () => void; onLogout: () => Promise<void>; notify: (message: string) => void }) {
  return <Menu.Root><Menu.Trigger className="button wallet-connected" aria-label={"Menu du wallet " + short(address)} data-layro="kokonut-labs/profile-dropdown">
    <AccountAvatar address={address} />{short(address)}<Icon name="chevron" size={13} />
  </Menu.Trigger><Menu.Portal><Menu.Positioner align="end" sideOffset={8} className="menu-positioner"><Menu.Popup className="profile-menu">
    <p className="menu-wallet">{short(address)}</p>
    <Menu.Item onClick={onAccount}><Icon name="user" size={16} />Votre compte</Menu.Item>
    <Menu.Item onClick={() => { void navigator.clipboard.writeText(address).then(() => notify("Adresse copiée.")).catch(() => notify("Copie indisponible. Retrouvez l’adresse dans Compte.")); }}><Icon name="copy" size={16} />Copier l’adresse</Menu.Item>
    <Menu.LinkItem href="https://app.hyperliquid.xyz/portfolio" target="_blank" rel="noreferrer"><Icon name="external" size={16} />Hyperliquid</Menu.LinkItem>
    <Menu.Separator />
    <Menu.Item onClick={() => { void onLogout().catch(() => notify("Déconnexion non confirmée.")); }}><Icon name="logout" size={16} />Déconnecter</Menu.Item>
  </Menu.Popup></Menu.Positioner></Menu.Portal></Menu.Root>;
}
// Layro: educlopez/wallet-card — controlled account data, privacy masking and copy action.
export function WalletCard({ owner, account, available, loading, children, notify }: {
  owner: string; account: string; available: number | undefined; loading: boolean; children: React.ReactNode; notify: (message: string) => void;
}) {
  const [hidden, setHidden] = useState(false);
  return <section className="panel account-card wallet-panel" data-layro="educlopez/wallet-card">
    <div className="wallet-panel-top"><AccountAvatar address={owner} interactive /><span className="network-label"><i />Mainnet</span></div>
    <h2>{short(owner)}</h2><p className="address">{owner}</p><StatusBadge tone="online">Identité vérifiée</StatusBadge>
    <div className="wallet-balance"><span>Solde spot disponible</span><button className="icon-btn" onClick={() => setHidden(!hidden)} aria-label={hidden ? "Afficher le solde" : "Masquer le solde"}><Icon name={hidden ? "hidden" : "eye"} size={17} /></button>
      <strong>{hidden ? "••••••" : usd(available)}<small>USDC</small></strong></div>
    {loading && <Skeleton label="Chargement du solde" compact />}
    <dl className="summary-list"><div><dt>Compte Hyperliquid</dt><dd>{short(account)}</dd></div><div><dt>Réseau</dt><dd>Mainnet</dd></div></dl>
    <button className="button secondary full" onClick={() => { void navigator.clipboard.writeText(owner).then(() => notify("Adresse copiée.")).catch(() => notify("Copie indisponible.")); }}><Icon name="copy" size={15} />Copier l’adresse</button>
    {children}
  </section>;
}
