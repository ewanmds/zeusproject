import { useRef, type PointerEvent as ReactPointerEvent } from "react";
import { Icon } from "../ui";

const strategies = [
  {
    icon: "auto",
    tag: "Scan",
    title: "Auto Outcome",
    body: "Le scanner choisit le marché. La session entre en maker et tourne uniquement à plat.",
  },
  {
    icon: "book",
    tag: "Carnet",
    title: "BBO",
    body: "Deux prix au sommet du carnet, recalculés à chaque mouvement du bid et de l'ask.",
  },
  {
    icon: "target",
    tag: "Distance",
    title: "MID",
    body: "Cotation à la distance choisie du prix médian, ajustée quand le carnet bouge.",
  },
] as const;

const guardrails = [
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
    title: "Post-only strict",
    body: "Un ordre qui traverserait le spread est refusé, jamais converti en taker.",
  },
  {
    icon: "stop",
    title: "Inventaire cloisonné",
    body: "Seules les positions ouvertes par la session sont clôturées à l'arrêt.",
  },
  {
    icon: "refresh",
    title: "Reprise vérifiée",
    body: "L'état de la session est contrôlé côté backend avant de reprendre.",
  },
  {
    icon: "wallet",
    title: "Clé sans retrait",
    body: "La clé de l'agent ne peut ni retirer ni transférer vos fonds.",
  },
] as const;

const allowed = [
  {
    title: "Passer des ordres maker",
    body: "Dans la session active, au prix que vous avez défini.",
  },
  {
    title: "Annuler les ordres",
    body: "À tout moment, sans attendre une exécution.",
  },
  {
    title: "Clôturer l'inventaire attribué",
    body: "Uniquement les positions ouvertes par cette session.",
  },
] as const;

const blocked = [
  {
    title: "Retirer ou transférer des fonds",
    body: "Aucun mouvement hors du compte Hyperliquid.",
  },
  {
    title: "Envoyer un ordre taker",
    body: "Le post-only refuse l'ordre avant qu'il ne traverse le spread.",
  },
  {
    title: "Toucher aux positions hors session",
    body: "Le reste de votre compte reste intact.",
  },
] as const;

const faq = [
  {
    q: "Que se passe-t-il si je ferme l'onglet ?",
    a: "La session continue côté backend. À la réouverture, elle reprend sans bouton — sauf après un arrêt dur, qui n'est jamais relancé automatiquement.",
  },
  {
    q: "Pourquoi un ordre peut-il être refusé ?",
    a: "Zeus vérifie le carnet, le spread, l'inventaire et les soldes avant chaque ordre. Un ordre qui ne respecte pas le cadre de la session est écarté, pas forcé.",
  },
  {
    q: "Comment arrêter une session ?",
    a: "Un clic annule les ordres de la session et clôture uniquement l'inventaire attribué. Les positions qui ne viennent pas de la session ne sont pas touchées.",
  },
  {
    q: "Qui choisit le marché ?",
    a: "En Auto Outcome, le scanner choisit le marché et la session tourne à plat. En BBO ou MID, vous désignez le marché ; les mêmes garde-fous s'appliquent.",
  },
] as const;

export function LandingView({
  onConnect,
  onHelp,
}: {
  onConnect: () => void;
  onHelp: () => void;
}) {
  const heroRef = useRef<HTMLElement | null>(null);

  function trackPointer(event: ReactPointerEvent<HTMLElement>) {
    if (event.pointerType !== "mouse") return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const hero = heroRef.current;
    if (!hero) return;
    const rect = hero.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const x = (event.clientX - rect.left) / rect.width - 0.5;
    const y = (event.clientY - rect.top) / rect.height - 0.5;
    hero.style.setProperty("--hero-px", x.toFixed(3));
    hero.style.setProperty("--hero-py", y.toFixed(3));
  }

  function resetPointer() {
    const hero = heroRef.current;
    if (!hero) return;
    hero.style.setProperty("--hero-px", "0");
    hero.style.setProperty("--hero-py", "0");
  }

  return (
    <div className="landing">
      <section
        className="landing-hero"
        ref={heroRef}
        onPointerMove={trackPointer}
        onPointerLeave={resetPointer}
      >
        <div className="hero-aurora" aria-hidden="true">
          <div className="aurora-field">
            <span className="aurora aurora-a" />
            <span className="aurora aurora-b" />
            <span className="aurora aurora-c" />
          </div>
          <span className="hero-grid" />
          <span className="hero-grain" />
        </div>

        <div className="landing-copy">
          <span className="landing-kicker">
            <Icon name="shield" size={14} />
            <span>Agent de market making</span>
            <span className="kicker-sep" aria-hidden="true" />
            <span className="kicker-meta">Hyperliquid</span>
          </span>
          <h1>
            Le market making,
            <span className="landing-accent"> sans surveillance.</span>
          </h1>
          <p className="landing-lead">
            Un agent qui cote dans le cadre que vous fixez, puis s'arrête
            proprement. Outcome et HIP-3, sans quitter Hyperliquid.
          </p>
          <div className="landing-actions">
            <button
              type="button"
              className="button primary landing-button"
              onClick={onConnect}
            >
              <Icon name="wallet" size={17} />
              Connecter mon wallet
            </button>
            <a
              className="button secondary landing-button landing-button-secondary"
              href="#comment-ca-marche"
            >
              Comment ça marche
              <Icon name="chevron" size={16} />
            </a>
          </div>
          <ul className="landing-proof" aria-label="Principes clés">
            <li>
              <Icon name="check" size={14} />
              Maker uniquement
            </li>
            <li>
              <Icon name="wallet" size={14} />
              Non-custodial
            </li>
            <li>
              <Icon name="sessions" size={14} />
              Sessions attribuées
            </li>
          </ul>
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
              <span className="preview-live preview-static">
                <i aria-hidden="true" />
                Aperçu
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
              Garde-fous actifs pendant toute la session.
            </div>
            <p className="preview-caption">
              Exemple de session — aucune donnée réelle.
            </p>
          </section>
          <span className="preview-float" aria-hidden="true">
            <span className="preview-float-dot" />
            Périmètre fermé
          </span>
        </div>
      </section>

      <section className="landing-section" aria-labelledby="features-title">
        <div className="landing-section-head">
          <div>
            <span className="section-eyebrow">Modes de cotation</span>
            <h2 id="features-title">Trois stratégies. Trois garde-fous.</h2>
          </div>
          <p>
            Un seul socle de contrôle. Vous choisissez la mécanique, Zeus
            applique les mêmes limites.
          </p>
        </div>
        <div className="feature-grid">
          {strategies.map((strategy, index) => (
            <article className="feature-card" key={strategy.title}>
              <div className="feature-top">
                <span className="feature-icon">
                  <Icon name={strategy.icon} size={18} />
                </span>
                <span className="feature-index">
                  {String(index + 1).padStart(2, "0")}
                </span>
              </div>
              <span className="feature-tag">{strategy.tag}</span>
              <h3>{strategy.title}</h3>
              <p>{strategy.body}</p>
            </article>
          ))}
        </div>
        <div className="guardrail-row">
          {guardrails.map((guardrail) => (
            <article className="guardrail-card" key={guardrail.title}>
              <span className="guardrail-icon">
                <Icon name={guardrail.icon} size={17} />
              </span>
              <div>
                <h3>{guardrail.title}</h3>
                <p>{guardrail.body}</p>
              </div>
            </article>
          ))}
        </div>
      </section>

      <section
        className="landing-section landing-steps-section"
        id="comment-ca-marche"
        aria-labelledby="steps-title"
      >
        <div className="landing-section-head">
          <div>
            <span className="section-eyebrow">Parcours</span>
            <h2 id="steps-title">Trois étapes.</h2>
          </div>
          <p>De la connexion au premier ordre, sans ligne de commande.</p>
        </div>
        <div className="landing-steps">
          <article className="landing-step">
            <span className="step-index">01</span>
            <h3>Connectez votre wallet</h3>
            <p>Signature de connexion. Aucun ordre n'est envoyé.</p>
          </article>
          <article className="landing-step">
            <span className="step-index">02</span>
            <h3>Autorisez l'agent</h3>
            <p>Une signature pour le trading. Vos fonds restent sur Hyperliquid.</p>
          </article>
          <article className="landing-step">
            <span className="step-index">03</span>
            <h3>Lancez la session</h3>
            <p>Le scanner choisit le marché. Vous suivez le volume et le PnL en direct.</p>
          </article>
        </div>
      </section>

      <section className="landing-section" aria-labelledby="scope-title">
        <div className="landing-section-head">
          <div>
            <span className="section-eyebrow">Périmètre</span>
            <h2 id="scope-title">Ce qu'il fait. Ce qu'il ne touche pas.</h2>
          </div>
          <p>
            Une clé d'agent limitée au trading de la session. Le reste de votre
            compte reste hors de portée.
          </p>
        </div>
        <div className="scope-grid">
          <article className="scope-card scope-can">
            <div className="scope-head">
              <span className="scope-icon">
                <Icon name="check" size={16} />
              </span>
              <h3>Autorisé</h3>
              <span className="scope-badge">Session</span>
            </div>
            <ul className="scope-list">
              {allowed.map((item) => (
                <li key={item.title}>
                  <span className="scope-mark">
                    <Icon name="check" size={13} />
                  </span>
                  <span>
                    <strong>{item.title}</strong>
                    <small>{item.body}</small>
                  </span>
                </li>
              ))}
            </ul>
          </article>
          <article className="scope-card scope-cannot">
            <div className="scope-head">
              <span className="scope-icon">
                <Icon name="close" size={16} />
              </span>
              <h3>Hors de portée</h3>
            </div>
            <ul className="scope-list">
              {blocked.map((item) => (
                <li key={item.title}>
                  <span className="scope-mark">
                    <Icon name="close" size={13} />
                  </span>
                  <span>
                    <strong>{item.title}</strong>
                    <small>{item.body}</small>
                  </span>
                </li>
              ))}
            </ul>
          </article>
        </div>
      </section>

      <section className="trust-band" aria-labelledby="trust-title">
        <div className="trust-intro">
          <span className="section-eyebrow">Sécurité</span>
          <h2 id="trust-title">Quatre limites explicites.</h2>
          <p>
            Elles sont vérifiées à chaque ordre, sans dépendre de votre
            présence.
          </p>
        </div>
        <div className="trust-grid">
          {trust.map((item) => (
            <div className="trust-item" key={item.title}>
              <Icon name={item.icon} size={18} />
              <div>
                <strong>{item.title}</strong>
                <span>{item.body}</span>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="landing-section" aria-labelledby="faq-title">
        <div className="landing-section-head">
          <div>
            <span className="section-eyebrow">FAQ</span>
            <h2 id="faq-title">Les questions qui restent.</h2>
          </div>
          <p>Quatre réponses courtes avant de connecter votre wallet.</p>
        </div>
        <div className="faq-grid">
          {faq.map((item) => (
            <details className="faq-item" key={item.q}>
              <summary>
                <span>{item.q}</span>
                <span className="faq-marker" aria-hidden="true">
                  <Icon name="chevron" size={15} />
                </span>
              </summary>
              <p>{item.a}</p>
            </details>
          ))}
        </div>
      </section>

      <section className="landing-cta" aria-labelledby="cta-title">
        <span className="section-eyebrow">Prêt ?</span>
        <h2 id="cta-title">Votre prochaine session.</h2>
        <p>Connectez votre wallet. Le cadre est prêt, l'arrêt aussi.</p>
        <div className="landing-actions">
          <button
            type="button"
            className="button primary landing-button"
            onClick={onConnect}
          >
            <Icon name="wallet" size={17} />
            Connecter mon wallet
          </button>
          <button
            type="button"
            className="button secondary landing-button"
            onClick={onHelp}
          >
            <Icon name="info" size={16} />
            Ouvrir le guide
          </button>
        </div>
      </section>
    </div>
  );
}
