import "./style.css";
import type { Page } from "./api";
import { setupPage } from "./pages/setup";
import { calibratePage } from "./pages/calibrate";
import { controlPage } from "./pages/control";
import { mapPage } from "./pages/map";
import { testPage } from "./pages/test";
import { homePage } from "./pages/home";
import { camcalPage } from "./pages/camcal";

const routes: Record<string, () => Page> = {
  "": homePage,
  setup: setupPage,
  calibrate: calibratePage,
  control: controlPage,
  map: mapPage,
  test: testPage,
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
