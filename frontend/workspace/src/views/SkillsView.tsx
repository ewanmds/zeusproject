import { Icon } from "../ui";

const skills = [
  {
    icon: "spark",
    title: "Design system",
    body: "Tokens de couleur, typographie, surfaces et espacements cohérents.",
  },
  {
    icon: "sessions",
    title: "Responsive",
    body: "Du mobile au grand écran, sans débordement ni navigation cassée.",
  },
  {
    icon: "shield",
    title: "Accessibilité",
    body: "Focus visible, contrastes, labels et respect de la réduction des animations.",
  },
  {
    icon: "auto",
    title: "Temps réel",
    body: "Snapshots WebSocket, garde de révision et états retardés explicites.",
  },
  {
    icon: "run",
    title: "Motion",
    body: "Transitions courtes sur transform et opacity, jamais sur les montants.",
  },
  {
    icon: "chart",
    title: "Performance",
    body: "Vues chargées à la demande, composants stables, pas de saut de mise en page.",
  },
  {
    icon: "wallet",
    title: "Sécurité UX",
    body: "Connexion wallet, autorisation d'agent et arrêt confirmé par snapshot.",
  },
  {
    icon: "book",
    title: "Données lisibles",
    body: "Tableaux triables, valeurs inconnues explicites, unités cohérentes.",
  },
] as const;

const principles = [
  ["Une action principale", "Chaque écran garde une décision claire."],
  ["Des chiffres stables", "Pas d'animation des montants, pas de saut de mise en page."],
  ["Aucune donnée inventée", "Une valeur inconnue reste inconnue."],
  ["L'état réel d'abord", "Les commandes sont confirmées par un snapshot, pas par optimisme."],
] as const;

const stack = [
  "React",
  "TypeScript",
  "Vite",
  "CSS tokens",
  "WebSocket",
  "Vitest",
];

export function SkillsView({
  connected,
  onConnect,
  onHome,
  onHelp,
}: {
  connected: boolean;
  onConnect: () => void;
  onHome: () => void;
  onHelp: () => void;
}) {
  return (
    <div className="landing skills-page">
      <section className="skills-hero">
        <div>
          <span className="landing-kicker">
            <Icon name="spark" size={15} />
            Skills frontend
          </span>
          <h1>Skills frontend.</h1>
          <p className="landing-lead">
            Les choix d'interface qui rendent Zeus lisible, rapide et sûr à
            utiliser.
          </p>
          <div className="landing-actions">
            <button className="button secondary landing-button" onClick={onHome}>
              {connected ? "Lancer une session" : "Retour à l'accueil"}
              <Icon name="arrow" size={16} />
            </button>
            {!connected && (
              <button
                className="button primary landing-button"
                onClick={onConnect}
              >
                <Icon name="wallet" size={17} />
                Connecter mon wallet
              </button>
            )}
          </div>
        </div>

        <aside className="system-card" aria-label="Aperçu du design system">
          <div className="system-head">
            <span>Zeus design system</span>
            <code>tokens.css</code>
          </div>
          <div className="swatches" aria-label="Palette">
            <i className="gold" role="img" aria-label="Or" />
            <i className="purple" role="img" aria-label="Violet" />
            <i className="green" role="img" aria-label="Vert" />
            <i className="surface" role="img" aria-label="Surface" />
          </div>
          <div className="type-samples">
            <strong>Exo 2</strong>
            <span>Orbitron</span>
            <code>JetBrains Mono</code>
          </div>
          <div className="system-foot">
            <span>Surfaces sombres</span>
            <span>Échelle cohérente</span>
            <span>Focus visible</span>
          </div>
        </aside>
      </section>

      <section className="landing-section" aria-labelledby="skills-title">
        <div className="landing-section-head">
          <h2 id="skills-title">Ce qui est construit.</h2>
          <p>Huit compétences appliquées au produit, pas à une maquette.</p>
        </div>
        <div className="skills-grid">
          {skills.map((skill) => (
            <article className="skill-card" key={skill.title}>
              <span className="feature-icon">
                <Icon name={skill.icon} size={18} />
              </span>
              <h3>{skill.title}</h3>
              <p>{skill.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="landing-section" aria-labelledby="principles-title">
        <div className="landing-section-head">
          <h2 id="principles-title">Les principes.</h2>
          <p>Ce qui décide à chaque écran.</p>
        </div>
        <div className="principles-grid">
          {principles.map(([title, body]) => (
            <article className="principle" key={title}>
              <h3>{title}</h3>
              <p>{body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="stack-band" aria-label="Stack frontend">
        {stack.map((item) => (
          <span key={item}>{item}</span>
        ))}
      </section>

      <section className="landing-cta" aria-labelledby="skills-cta-title">
        <h2 id="skills-cta-title">Le résultat, en direct.</h2>
        <p>Connectez votre wallet et ouvrez le workspace.</p>
        <div className="landing-actions">
          {connected ? (
            <button className="button primary landing-button" onClick={onHome}>
              Lancer une session
              <Icon name="arrow" size={16} />
            </button>
          ) : (
            <button
              className="button primary landing-button"
              onClick={onConnect}
            >
              <Icon name="wallet" size={17} />
              Connecter mon wallet
            </button>
          )}
          <button className="button secondary landing-button" onClick={onHelp}>
            Comment ça marche ?
          </button>
        </div>
      </section>
    </div>
  );
}
