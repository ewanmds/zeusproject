import { Icon, Empty } from "../ui";
import { FeedbackButton } from "../components/FeedbackButton";
import { WalletCard } from "../components/workspace/WalletPanel";
import { SessionStepper } from "../components/layout";
import { InlineAlert } from "../components/feedback";
import { short } from "../data";
import type { AccountProps } from "../viewTypes";
export function AccountView({
  actionError,
  owner,
  w,
  ready,
  busy,
  authorize,
  navigate,
  setNotice,
  setWalletModal,
}: AccountProps) {
  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">VOTRE WORKSPACE</span>
          <h1>Votre compte.</h1>
        </div>
      </div>
      {owner ? (
        <div className="account-grid">
          <WalletCard key={owner} owner={owner} account={w.identity!.account} available={w.balance?.usdc_available}
            loading={!w.balance} notify={setNotice}>
            <button
              className="button secondary full"
              onClick={async () => {
                await w.logout();
                setNotice("Déconnecté. Vos bots serveur conservent leur état.");
              }}
            >
              <Icon name="logout" size={16} />
              Déconnecter le wallet
            </button>
          </WalletCard>
          <section className="panel authorization">
            <SessionStepper connected ready={ready} />
            <span className="eyebrow">AUTORISATION HYPERLIQUID</span>
            <h2>
              Connecté.
              <br />
              {ready ? "Prêt à lancer." : "Une dernière étape."}
            </h2>
            <p>
              {ready
                ? "Votre agent de trading est autorisé pour ce compte."
                : "Autorisez un agent dédié à Zeus pour permettre aux bots de placer et d’annuler leurs ordres."}
            </p>
            <div className="permission-list">
              <span>
                <Icon name="check" size={16} />
                Identité du wallet vérifiée
              </span>
              <span>
                <Icon name={ready ? "check" : "clock"} size={16} />
                {ready
                  ? "Agent de trading vérifié"
                  : "Autorisation à signer dans votre wallet"}
              </span>
              <span>
                <Icon name="shield" size={16} />
                Clé principale conservée dans votre wallet
              </span>
            </div>
            {(w.balance?.agent || w.identity?.agent) && (
              <div className="agent-address">
                <span>Agent</span>
                <code>
                  {short((w.balance?.agent || w.identity?.agent)!.address)}
                </code>
                <span className={ready ? "positive" : "gold"}>
                  {ready ? "Autorisé" : "À vérifier"}
                </span>
              </div>
            )}
            <FeedbackButton className="button primary full"
              onClick={ready ? () => navigate("run") : authorize}
              disabled={!!busy} loading={busy === "agent"} loadingLabel="Autorisation en cours">
              {ready ? "Préparer une session" : "Autoriser le trading"}
              <Icon name="arrow" size={17} />
            </FeedbackButton>
            {actionError && <InlineAlert>{actionError}</InlineAlert>}
            <button className="text-link" onClick={w.refresh}>
              Actualiser l’autorisation
              <Icon name="refresh" size={13} />
            </button>
          </section>
          <section className="panel account-help">
            <Icon name="info" />
            <div>
              <h3>Le compte, le wallet et l’agent</h3>
              <p>
                Votre wallet vous identifie. L’agent signe les ordres du bot.
                Les soldes et les fills appartiennent à votre compte
                Hyperliquid.
              </p>
            </div>
            <a
              href="https://app.hyperliquid.xyz/API"
              target="_blank"
              rel="noreferrer"
            >
              Gérer sur Hyperliquid
              <Icon name="arrow" size={15} />
            </a>
          </section>
        </div>
      ) : (
        <Empty
          icon="wallet"
          title="Un espace lié à votre wallet."
          text="Vos réglages, vos comptes et vos sessions restent réunis ici."
          action={
            <button
              className="button primary"
              onClick={() => setWalletModal(true)}
            >
              Connecter mon wallet
            </button>
          }
        />
      )}
    </>
  );
}
