/* eslint-disable react-refresh/only-export-components */
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { zh } from "./workspace.zh";

type Theme = "light" | "dark";
type Language = "en" | "zh-CN";
type Preferences = { theme: Theme; language: Language; setTheme: (theme: Theme) => void; setLanguage: (lang: Language) => void };
const defaults: Preferences = { theme: "light", language: "en", setTheme: () => {}, setLanguage: () => {} };
const Context = createContext(defaults);

function readPreference(key: string) {
  try { return localStorage.getItem(key); } catch { return null; }
}

export function WorkspacePreferences({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(() => readPreference("oasis-theme") === "dark" ? "dark" : "light");
  const [language, setLanguage] = useState<Language>(() => readPreference("oasis-language") === "zh-CN" ? "zh-CN" : "en");
  useEffect(() => {
    document.documentElement.dataset.oasisTheme = theme;
    document.documentElement.lang = language;
    try {
      localStorage.setItem("oasis-theme", theme);
      localStorage.setItem("oasis-language", language);
    } catch {
      // The current workspace remains usable without browser persistence.
    }
  }, [theme, language]);
  return <Context.Provider value={{ theme, language, setTheme, setLanguage }}>{children}</Context.Provider>;
}

export function useWorkspacePreferences() {
  const preferences = useContext(Context);
  return { ...preferences, t: (text: string) => preferences.language === "zh-CN" ? zh[text] ?? text : text };
}
