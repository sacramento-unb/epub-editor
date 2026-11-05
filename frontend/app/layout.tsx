import type { Metadata } from "next";
import "./globals.css";
import { ThemeToggle } from "@/components/ThemeToggle";

export const metadata: Metadata = { title: "Conversor PDF → EPUB", description: "Converta e edite PDFs como EPUB 3" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="pt-BR" suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `try{var t=localStorage.getItem('tema');if(t)document.documentElement.dataset.theme=t}catch(e){}`,
          }}
        />
      </head>
      <body>
        <header className="topbar">
          <a className="brand" href="/">PDF → EPUB</a>
          <nav aria-label="Navegação principal">
            <a href="/">Projetos</a>
            <a href="/settings">Configurações de IA</a>
          </nav>
          <ThemeToggle />
        </header>
        {children}
      </body>
    </html>
  );
}
