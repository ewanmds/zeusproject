import { Icon } from "../ui";

const features = [
  {
    icon: "auto",
    title: "Auto Outcome",
    body: "Le scanner choisit le marché. La session entre en maker et tourne uniquement à plat.",
  },
  {
    icon: "book",
    title: "BBO",
    body: "Cotation au meilleur bid et au meilleur ask, toujours en post-only.",
  },
  {
    icon: "target",
    title: "MID",
    body: "Cotation à la distance choisie du prix médian, avec les mêmes garde-fous.",
  },
  {
    icon: "stop",
    title: "Arrêt instantané",
    body: "Un clic annule les ordres de la session et clôture seulement son inventaire attribué.",
  },
  {
    icon: "shield",
    title: "Contrôles de risque",
    body: "Stop inventaire, spread maximal, carnet frais et soldes vérifiés avant chaque ordre.",
  },
  {
    icon: "refresh",
    title: "Reprise backend",
    body: "Une session interrompue reprend sans bouton. Un arrêt dur n'est jamais relancé.",
  },
] as const;

const trust = [
  {
    icon: "shield",
    title: "Post-only",
    body: "Aucun ordre taker non autorisé.",
  },
  {
    icon: "target",
    title: "Rotation à plat",
    body: "Jamais de liquidation par rotation.",
  },
  {
    icon: "stop",
    title: "Arrêt attribué",
    body: "Seul l'inventaire de la session est vendu.",
  },
  {
    icon: "wallet",
    title: "Non-custodial",
    body: "Vos fonds restent sur Hyperliquid.",
  },
] as const;

export function LandingView({
  onConnect,
  onHelp,
}: {
  onConnect: () => void;
  onHelp: () => void;
}) {
  return (
    <div className="landing">
      <section className="landing-hero">
        <div className="landing-copy">
          <span className="landing-kicker">
            <Icon name="auto" size={15} />
            Market making Hyperliquid
          </span>
          <h1>
            Le market making,
            <span className="landing-accent"> sans surveillance.</span>
          </h1>
          <p className="landing-lead">
            Des sessions post-only sur Outcome et HIP-3, avec rotation à plat et
            arrêt attribué.
          </p>
          <div className="landing-actions">
            <button
              className="button primary landing-button"
              onClick={onConnect}
            >
              <Icon name="wallet" size={17} />
              Connecter mon wallet
            </button>
          </div>
          <p className="landing-note">
            <Icon name="shield" size={15} />
            Non-custodial. Vos fonds restent sur Hyperliquid.
          </p>
        </div>

        <div className="landing-visual">
          <section
            className="session-preview"
            aria-label="Aperçu d'une session Zeus"
          >
            <div className="preview-head">
              <span className="preview-title">
                <span className="preview-icon">
                  <Icon name="auto" size={17} />
                </span>
                Session Auto Outcome
              </span>
              <span className="preview-live">
                <i aria-hidden="true" />
                En direct
              </span>
            </div>
            <div className="preview-market">
              <span>Marché</span>
              <strong>Sélection automatique</strong>
            </div>
            <div className="preview-book" aria-hidden="true">
              <div className="book-line">
                <span>BID</span>
                <div className="book-track">
                  <i className="book-fill bid" />
                </div>
              </div>
              <div className="book-line">
                <span>ASK</span>
                <div className="book-track">
                  <i className="book-fill ask" />
                </div>
              </div>
            </div>
            <div className="preview-grid">
              <div>
                <span>Exécution</span>
                <strong>Maker</strong>
              </div>
              <div>
                <span>Rotation</span>
                <strong>À plat</strong>
              </div>
              <div>
                <span>Arrêt</span>
                <strong>Attribué</strong>
              </div>
              <div>
                <span>Journal</span>
                <strong>Durable</strong>
              </div>
            </div>
            <div className="preview-foot">
              <Icon name="shield" size={14} />
              Post-only. Aucun ordre taker non autorisé.
            </div>
          </section>
        </div>
      </section>

      <section className="landing-section" aria-labelledby="features-title">
        <div className="landing-section-head">
          <h2 id="features-title">Ce que Zeus exécute.</h2>
          <p>Trois stratégies, un seul niveau de contrôle.</p>
        </div>
        <div className="feature-grid">
          {features.map((feature) => (
            <article className="feature-card" key={feature.title}>
              <span className="feature-icon">
                <Icon name={feature.icon} size={18} />
              </span>
              <h3>{feature.title}</h3>
              <p>{feature.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="landing-section" aria-labelledby="steps-title">
        <div className="landing-section-head">
          <h2 id="steps-title">Trois étapes.</h2>
          <p>De la connexion au premier ordre.</p>
        </div>
        <div className="landing-steps">
          <article className="landing-step">
            <span>01</span>
            <h3>Connectez votre wallet</h3>
            <p>Signature de connexion. Aucun ordre n'est envoyé.</p>
          </article>
          <article className="landing-step">
            <span>02</span>
            <h3>Autorisez l'agent</h3>
            <p>Une signature pour le trading. Vos fonds restent sur Hyperliquid.</p>
          </article>
          <article className="landing-step">
            <span>03</span>
            <h3>Lancez la session</h3>
            <p>Le scanner choisit le marché. Vous suivez le volume et le PnL en direct.</p>
          </article>
        </div>
      </section>

      <section className="trust-band" aria-label="Principes de sécurité">
        {trust.map((item) => (
          <div key={item.title}>
            <Icon name={item.icon} size={18} />
            <strong>{item.title}</strong>
            <span>{item.body}</span>
          </div>
        ))}
      </section>

      <section className="landing-cta" aria-labelledby="cta-title">
        <h2 id="cta-title">Votre prochaine session.</h2>
        <p>Connectez votre wallet et gardez le contrôle sur chaque ordre.</p>
        <div className="landing-actions">
          <button className="button primary landing-button" onClick={onConnect}>
            <Icon name="wallet" size={17} />
            Connecter mon wallet
          </button>
          <button className="button secondary landing-button" onClick={onHelp}>
            Comment ça marche ?
          </button>
        </div>
      </section>
    </div>
  );
}
