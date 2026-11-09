import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./tokens.css";
import "./styles.css";
import "./brand.css";
import "./components.css";
class Boundary extends React.Component<
  { children: React.ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <div className="fatal">
        <span className="brand-mark">Z</span>
        <h1>Reprenons là où vous étiez.</h1>
        <p>
          Un problème d’affichage est survenu. Vos sessions serveur conservent
          leur état.
        </p>
        <button className="button primary" onClick={() => location.reload()}>
          Recharger le workspace
        </button>
      </div>
    ) : (
      this.props.children
    );
  }
}
createRoot(document.getElementById("root")!).render(
  <Boundary>
    <App />
  </Boundary>,
);
