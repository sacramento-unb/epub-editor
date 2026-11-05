"use client";
import { useEffect, useState } from "react";

export function ThemeToggle() {
  const [theme, setTheme] = useState<string>("auto");
  useEffect(() => {
    try { setTheme(localStorage.getItem("tema") || "auto"); } catch { /* sem armazenamento */ }
  }, []);
  function change(t: string) {
    setTheme(t);
    try {
      if (t === "auto") { localStorage.removeItem("tema"); delete document.documentElement.dataset.theme; }
      else { localStorage.setItem("tema", t); document.documentElement.dataset.theme = t; }
    } catch { document.documentElement.dataset.theme = t; }
  }
  return (
    <label className="row" style={{ fontSize: 12 }}>
      Tema
      <select aria-label="Tema da interface" value={theme} onChange={(e) => change(e.target.value)}>
        <option value="auto">Sistema</option>
        <option value="light">Claro</option>
        <option value="dark">Escuro</option>
      </select>
    </label>
  );
}
