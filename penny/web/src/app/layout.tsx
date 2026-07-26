import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Penny — WhatsApp Console",
  description: "Programmatic control of a WhatsApp Business account",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="h-full overflow-hidden">{children}</body>
    </html>
  );
}
