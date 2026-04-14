import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ZT411 Troubleshooter",
  description: "Agentic AI troubleshooting console for the Zebra ZT411",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-gray-50 text-gray-900 antialiased">
        <header className="bg-brand text-white shadow-md">
          <div className="mx-auto max-w-7xl px-4 py-3 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <span className="text-xl font-bold tracking-tight">ZT411 Troubleshooter</span>
              <span className="text-xs bg-white/20 rounded px-2 py-0.5">AI Agent</span>
            </div>
            <nav className="flex gap-6 text-sm font-medium">
              <a href="/" className="hover:text-white/80 transition-colors">Sessions</a>
              <a href="/admin" className="hover:text-white/80 transition-colors">Admin</a>
            </nav>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
