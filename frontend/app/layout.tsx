import type { Metadata } from "next";
import "./globals.css";
import { Shell } from "@/components/shell";

export const metadata: Metadata = {
  title: "NimbusCRM Assistant",
  description: "GenAI CRM assistant: ask about accounts, deals and tickets, product help and policies, and run CRM workflows.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <Shell>{children}</Shell>
      </body>
    </html>
  );
}
