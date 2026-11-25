import "./style.css";
import { api, currentTree, h, setCurrentTree, type Page } from "./api";
import { setupPage } from "./pages/setup";
import { calibratePage } from "./pages/calibrate";
import { controlPage } from "./pages/control";
import { mapPage } from "./pages/map";
import { testPage } from "./pages/test";
import { effectsPage } from "./pages/effects";
import { homePage } from "./pages/home";
import { camcalPage } from "./pages/camcal";
import { scanPage } from "./pages/scan";
import { schedulePage } from "./pages/schedule";

const routes: Record<string, () => Page> = {
  "": homePage,
  setup: setupPage,
  scan: scanPage,
  calibrate: calibratePage,
  control: controlPage,
  map: mapPage,
  test: testPage,
  effects: effectsPage,
  schedule: schedulePage,
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

// The tree the pages act on (shown when there is more than one tree).
const treePick = document.getElementById("tree-pick") as HTMLSelectElement;

async function loadTrees(): Promise<void> {
  try {
    const trees = await api<{ id: string; name: string; default: boolean }[]>("/api/trees");
    const cur = currentTree();
    const known = trees.some((t) => t.id === cur);
    if (cur && !known) setCurrentTree(null);
    treePick.replaceChildren(...trees.map((t) => h("option", { value: t.id }, t.name)));
    treePick.value = known && cur ? cur : (trees.find((t) => t.default) ?? trees[0]).id;
    treePick.hidden = trees.length < 2;
  } catch {
    treePick.hidden = true;
  }
}

treePick.onchange = () => {
  setCurrentTree(treePick.value);
  const hash = location.hash.replace(/([?&])tree=[^&]*&?/, "$1").replace(/[?&]$/, "");
  if (hash !== location.hash) location.hash = hash; // routes again
  else route();
};

window.addEventListener("trees-changed", () => void loadTrees());
window.addEventListener("hashchange", () => {
  if (/[?&]tree=/.test(location.hash)) void loadTrees(); // a link to another tree's page
  route();
});
loadTrees().finally(route);
