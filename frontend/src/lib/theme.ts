export type Theme = "light" | "dark";

export const THEME_KEY = "rag-theme";

/** Runs before first paint (inlined in the root layout) so there is no flash of the wrong theme. Dark by default. */
export const themeInitScript = `(function(){try{var t=localStorage.getItem('${THEME_KEY}');if(t!=='light'&&t!=='dark')t='dark';document.documentElement.classList.toggle('dark',t==='dark')}catch(e){document.documentElement.classList.add('dark')}})();`;

export function currentTheme(): Theme {
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

const THEME_EVENT = "rag:theme";

export function setTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* private mode: the choice lasts for this page only */
  }
  window.dispatchEvent(new Event(THEME_EVENT));
}

/** For useSyncExternalStore: the theme lives on <html>, outside React. */
export function subscribeTheme(callback: () => void): () => void {
  window.addEventListener(THEME_EVENT, callback);
  return () => window.removeEventListener(THEME_EVENT, callback);
}
