import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Alpha Trader AI",
  description: "Analisi e supporto al trading — Research & Paper Trading. Nessun rendimento è garantito.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="it">
      <body className="min-h-screen font-sans antialiased">{children}</body>
    </html>
  );
}
