import "./style.css";
import type { Page } from "./api";
import { setupPage } from "./pages/setup";
import { calibratePage } from "./pages/calibrate";
import { controlPage } from "./pages/control";
import { mapPage } from "./pages/map";
import { testPage } from "./pages/test";
import { effectsPage } from "./pages/effects";
import { homePage } from "./pages/home";
import { camcalPage } from "./pages/camcal";
import { scanPage } from "./pages/scan";

const routes: Record<string, () => Page> = {
  "": homePage,
  setup: setupPage,
  scan: scanPage,
  calibrate: calibratePage,
  control: controlPage,
  map: mapPage,
  test: testPage,
  effects: effectsPage,
  camcal: camcalPage,
};

let current: Page | null = null;

function route(): void {
  const name = location.hash.replace(/^#\/?/, "").split("?")[0];
  const make = routes[name] ?? homePage;
  current?.unmount?.();
  const root = document.getElementById("app")!;
  root.replaceChildren();
  current = make();
  current.mount(root);
  for (const a of document.querySelectorAll<HTMLAnchorElement>("#nav a")) {
    a.classList.toggle("active", a.getAttribute("href") === `#/${name}`);
  }
}

window.addEventListener("hashchange", route);
route();
